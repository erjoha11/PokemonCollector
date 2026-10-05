import type { CapturedComment, CapturedReply, PostCapture } from "../shared/capture";
import { parseAmount } from "./amount";
import { judgeBidTimes, replyWindow, type TimeVerdict, type TimeWindow } from "./bidTime";
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
  /**
   * Doesn't count because it came too late (#329): the seller said so in a reply under it
   * ("seller"), or its time proves it came after the lot's end ("time"). Null otherwise.
   */
  late: "seller" | "time" | null;
  /** Its time vs the end: "unsure" when its time window straddles the end (kept valid, the lot is flagged). Null without an end time. */
  timeVerdict: TimeVerdict | null;
  /** The seller's reply directly under this bid, if any: its raw text and how it was read. */
  sellerReply: SellerReply | null;
};

/**
 * The seller's reply under a bid (#329): "too-late" (the bid doesn't count: "for sent", "auksjonen
 * er avsluttet", "ikke gyldig"), "ok" (accepts it, congratulates, "sendt PM", a photo), or
 * "unsure". An unsure reply under your own bid goes to Claude (`viaClaude` once it answered).
 */
export type SellerReply = { text: string; verdict: "too-late" | "ok" | "unsure"; viaClaude: boolean };

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

/** Claude's reading of a claim lot (photo + replies): every card, its price, who got it (null = for sale). */
export type ClaimCards = { card: string; price: number | null; claimedBy: string | null; isMe: boolean }[];

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
  /** From Claude, when asked: every card in the photo, its price, and who got it (null = still for sale). */
  claimCards: ClaimCards | null;
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
  /**
   * Auctions with a known end: the lot's end with chained antisnipe, at its latest possible
   * (`judgeBidTimes`' endHi), in ms. Null without an end time (or for claim lots).
   */
  closesAt?: number | null;
  /**
   * Why the result needs a look (#329), or null: a bid that counts may have come after the end
   * (its time couldn't be proven either way), or the seller replied under your bid and that
   * reply couldn't be read (yet).
   */
  lateCheck?: string | null;
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

/** "MP: 1400", "Mp 10kr", "Holo, mp 30kr", "Minstepris 500", or a bare "700kr" on its own line. */
function lotStartBid(text: string): number | null {
  const m = text.match(/(?<![\p{L}\d])(?:mp|minstepris|startbud)\s*:?\s*(\d[^\n]*)/iu);
  if (m) return parseAmount(m[1]);
  const bare = text.match(/(?:^|\n)\s*(\d[\d .]*)\s*(?:kr|,-)\s*(?:\n|$)/i);
  return bare ? parseAmount(bare[1]) : null;
}

/** The lot's title: its own text, else Claude's name for the photo, else "Lot N". */
function lotTitleFor(c: CapturedComment, position: number, options: LotOptions): { title: string; untitled: boolean; namedByClaude: boolean } {
  const title = lotTitle(c.text, position);
  const untitled = title === `Lot ${position}`;
  const photo = c.images[0]?.src;
  const name = untitled && photo ? options.lotName?.(fullSizePhoto(photo)) : undefined;
  return name ? { title: name, untitled, namedByClaude: true } : { title, untitled, namedByClaude: false };
}

/** Lots whose text doesn't name them but which have a photo: Claude can name them from it. */
export function untitledLotPhotos(capture: PostCapture): string[] {
  const seller = sellerOf(capture);
  return capture.comments
    .filter((c) => isLot(c, seller) && c.images[0]?.src)
    .filter((c, i) => lotTitle(c.text, i + 1) === `Lot ${i + 1}`)
    .map((c) => fullSizePhoto(c.images[0].src));
}

/** The lot's first line, unless it's only a price ("Mp 15kr"); then "Lot N". */
function lotTitle(text: string, position: number): string {
  const first = text.split("\n")[0]?.trim() ?? "";
  const onlyPrice = /^(?:mp|mb|minstepris)?\s*:?\s*\d[\d .,]*\s*(?:kr|,-|&)?\.?$/i.test(first);
  return first && !onlyPrice ? first : `Lot ${position}`;
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
  /** Claude's name for a lot's photo (by its full-size URL), when the lot's text doesn't name it. */
  lotName?: (imageUrl: string) => string | null | undefined;
  /** Claude's reading of a claim lot (see claimLotInput), or undefined when not asked yet. */
  claimAnswer?: (input: ClaimLotInput) => { cards: { card: string; price: number | null; claimedBy: string | null }[] } | undefined;
  /** Claude's reading of a reply the rules weren't sure about: an amount, null (not a bid), or undefined (not asked yet). */
  answer?: (seller: string | null, text: string) => number | null | undefined;
  /** The sale's end (ms; rules, else Claude's reading), for bid times vs the end (#329). Unknown: no time checks. */
  endsAt?: number | null;
  /** Antisnipe minutes ("Antisnipe 5 min: Ja" → 5; 0 or null = none). */
  softCloseMinutes?: number | null;
  /**
   * Claude's reading of a seller's reply under your bid that the rules couldn't classify: true =
   * the seller rejects the bid (too late, ended, not valid), false = not, null = can't tell,
   * undefined = not asked yet.
   */
  sellerReplyAnswer?: (text: string) => boolean | null | undefined;
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
  // A reply's age ("5 min") is relative to the read that saw it: `seenAt` on merged reads, else
  // the one read there was. Older merged reads without it: only "it was there by the last read".
  const readAt = Date.parse(capture.capturedAt);
  const singleReadAt = (capture.reads ?? 1) <= 1 ? readAt : null;
  for (const c of whole ? [whole.lot] : lotComments) {
    const startBid = lotStartBid(c.text) ?? options.listingMinPrice;
    const increment = lotIncrement(c.text) ?? options.listingIncrement;
    const replies = whole ? whole.replies : [...c.replies].sort((a, b) => compareIds(a.id, b.id));
    const bids: Bid[] = [];
    /** When each bid was placed, as a window (src/domain/bidTime.ts). */
    const bidWindows = new Map<Bid, TimeWindow>();
    let unsureCount = 0;
    /** Replies that may be bids but couldn't be counted: their IDs (null = order unknown). */
    const doubtful: (string | null)[] = [];
    const claims = options.claims ? readClaims(replies, seller, me) : [];
    const mineClaims = claims.filter((x) => x.isMe);
    let myClaim: MyClaim = mineClaims.length === 0 ? "none" : mineClaims.some((x) => !x.contested) ? "claimed" : "check";
    // Claude has read the photo and replies: it decides who got what (and the prices).
    let claimCards: ClaimCards | null = null;
    const lotInput = options.claims ? claimLotInput(c, seller) : null;
    const answer = lotInput ? options.claimAnswer?.(lotInput) : undefined;
    const textPrice = options.claims ? lotTextPrice(c.text) : null;
    if (answer?.cards) {
      // A card whose price Claude couldn't read from the photo takes the lot's per-card text price.
      const fallback = textPrice?.perCard || answer.cards.length === 1 ? (textPrice?.kr ?? null) : null;
      claimCards = answer.cards.map((x) => ({
        ...x,
        price: x.price ?? fallback,
        isMe: !!me && !!x.claimedBy && normalizeName(x.claimedBy) === me,
      }));
      if (mineClaims.length > 0) myClaim = claimCards.some((x) => x.isMe) ? "claimed" : "check";
    }
    for (const r of options.claims ? [] : replies) {
      const bid = toBid(r, seller, me, options, !!whole && whole.underReply.has(r.id));
      if (bid === "unsure") {
        unsureCount++;
        if (normalizeName(r.author) !== me) doubtful.push(r.id);
      } else if (bid) {
        if (bid.replyId === null && !bid.isMe) doubtful.push(null); // Can't tell if it came before or after yours.
        bids.push(bid);
        bidWindows.set(bid, replyWindow(r.timeText, r.seenAt ? Date.parse(r.seenAt) : singleReadAt, readAt));
      }
    }
    // The seller's replies under bids: one saying it came too late makes that bid not count (#329).
    if (!options.claims) applySellerReplies(replies, seller, bids, options);
    // Validity, in the order bids were placed: on the lot itself (sellers reject bids placed
    // under another reply: "bud blir bare godtatt under hovedbildet"), not rejected by the seller
    // as too late, not placed after the end (chained antisnipe, src/domain/bidTime.ts), at least
    // the start bid, and at least the current highest plus the increment (above it when none is
    // stated). Returns whether the bid counts (and so can move the end, with antisnipe).
    let highest: Bid | null = null;
    const judge = (b: Bid, time: TimeVerdict | null): boolean => {
      b.timeVerdict = time;
      if (b.amount === null) return false;
      if (b.underReply || b.late === "seller") {
        b.valid = false;
      } else if (time === "late") {
        b.valid = false;
        b.late = "time";
        b.note = "after the end (its time is past the lot's end, antisnipe included)";
      } else if (startBid !== null && b.amount < startBid) {
        b.valid = false;
        b.note = `below the start bid (${startBid})`;
      } else if (highest && b.amount < (highest.amount ?? 0) + (increment ?? 1)) {
        b.valid = false;
        b.note = increment ? `less than ${increment} over the highest bid` : "not over the highest bid";
      } else {
        highest = b;
        return true;
      }
      return false;
    };
    let closesAt: number | null = null;
    if (!options.claims && options.endsAt != null) {
      const windows = bids.map((b) => bidWindows.get(b) ?? { lo: -Infinity, hi: Infinity });
      closesAt = judgeBidTimes(options.endsAt, options.softCloseMinutes ?? null, windows, (i, v) => judge(bids[i], v)).endHi;
    } else {
      for (const b of bids) judge(b, null);
    }
    // Nobody reached the start bid: take the best of the rest, flagged, since sellers sometimes
    // accept it ("den er grei"). Same order and increment rule, without the start bid.
    const counted = (b: Bid) => b.amount !== null && !b.underReply && !b.late;
    const belowStart = !highest && bids.some(counted);
    if (belowStart) {
      for (const b of bids) {
        if (!counted(b)) continue;
        if (!highest || b.amount! >= (highest.amount ?? 0) + (increment ?? 1)) {
          highest = b;
          b.valid = true;
          b.note = `below the start bid (${startBid})`;
        }
      }
    }
    const mine = bids.filter((b) => b.isMe && b.amount !== null);
    const myHighestBid = mine.length ? Math.max(...mine.map((b) => b.amount!)) : null;
    lots.push({
      closesAt,
      lateCheck: lateCheckFor(bids, highest),
      commentId: c.id,
      position: lots.length + 1,
      ...(whole ? { title: whole.title, untitled: false, namedByClaude: false } : lotTitleFor(c, lots.length + 1, options)),
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

export type ClaimLotInput = {
  seller: string | null;
  imageUrl: string;
  replies: { author: string; text: string }[];
  /** The lot comment's own text: some sellers write the price there ("10kr per stk"). */
  lotText?: string;
};

/** The full-size version of a Facebook CDN photo: the `ctp` parameter asks for a small crop. */
export const fullSizePhoto = (url: string) => url.replace(/([?&])ctp=[^&]*&?/, "$1").replace(/[?&]$/, "");

/** What Claude needs for a claim lot: its full-size photo and the replies (not the seller's), oldest first. */
export function claimLotInput(c: CapturedComment, seller: string | null): ClaimLotInput | null {
  const photo = c.images[0]?.src;
  if (!photo) return null;
  const replies = [...c.replies]
    .sort((a, b) => compareIds(a.id, b.id))
    .filter((r) => !seller || normalizeName(r.author) !== normalizeName(seller))
    .map((r) => ({ author: r.author ?? "", text: r.text }));
  return { seller, imageUrl: fullSizePhoto(photo), replies, lotText: c.text };
}

/** Claim lots you've claimed on (photo prices, who got what). */
export function myClaimLots(capture: PostCapture, myName: string): ClaimLotInput[] {
  const seller = sellerOf(capture);
  const me = normalizeName(myName);
  return capture.comments
    .filter((c) => isLot(c, seller) && c.replies.some((r) => normalizeName(r.author) === me && claimItems(r.text, seller)))
    .flatMap((c) => claimLotInput(c, seller) ?? []);
}

/** Every lot in a claim sale, yours first: what's taken and what's still for sale. */
export function claimLotsToRead(capture: PostCapture, myName: string): ClaimLotInput[] {
  const seller = sellerOf(capture);
  const mine = myClaimLots(capture, myName);
  const mineUrls = new Set(mine.map((x) => x.imageUrl));
  const rest = capture.comments
    .filter((c) => isLot(c, seller))
    .flatMap((c) => claimLotInput(c, seller) ?? [])
    .filter((x) => !mineUrls.has(x.imageUrl));
  return [...mine, ...rest];
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

/**
 * Why a lot's result needs a look (#329): the bid that counts as highest, or one of yours that
 * counts, may have come after the end (its time window straddles it: kept valid, never guessed
 * late); or the seller replied under your bid and neither the rules nor Claude (yet) could read it.
 */
function lateCheckFor(bids: Bid[], highest: Bid | null): string | null {
  const unread = bids.find((b) => b.isMe && b.sellerReply?.verdict === "unsure");
  if (unread) return `the seller replied under your bid: "${unread.sellerReply!.text.trim()}"`;
  const unsure = bids.filter((b) => b.valid && b.timeVerdict === "unsure" && (b === highest || b.isMe));
  if (unsure.length === 0) return null;
  return unsure.some((b) => b === highest) ? "the highest bid may have come after the end" : "your bid may have come after the end";
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
    late: null,
    timeVerdict: null,
    sellerReply: null,
  };
}

// ── The seller's replies under bids (#329) ─────────────────────────────────────────────────
// Sellers reject late bids in a reply under them; the wording varies ("for sent", "kom for seint",
// "auksjonen er avsluttet", "etter sluttid", "ikke gyldig"). "ikke for sent" ("not too late") isn't one.

const TOO_LATE = new RegExp(
  [
    String.raw`(?<!ikke\s)\bfor\s+(?:sent|seint|sen)\b`,
    String.raw`\bkom\s+(?:litt\s+|alt\s+)?(?:for\s+)?(?:sent|seint|etter)\b`,
    String.raw`\better\s+(?:slutt(?:tid(?:en)?)?|tiden|fristen|deadline)\b`,
    String.raw`\bauksjon(?:en)?\s+(?:er\s+|var\s+|ble\s+|har\s+)?(?:avsluttet|avslutta|ferdig|slutt|over|stengt)\b`,
    String.raw`\b(?:ikke\s+gyldig|ugyldig|teller\s+ikke|gjelder\s+ikke)\b`,
    String.raw`\btoo\s+late\b`,
    String.raw`\b(?:auction\s+(?:is\s+|has\s+)?(?:over|ended|closed)|not\s+valid)\b`,
  ].join("|"),
  "iu",
);
/** Words of a seller who's accepting the bid or winding up the sale with its winner. */
const ACCEPTS = /\b(?:ikke\s+for\s+(?:sent|seint)|(?:den|budet)\s+(?:teller|gjelder|står)|gratulerer|grattis|gratz|congrats|vant|vinner(?:en)?|er\s+dine?|fikk\s+(?:den|det|kortet|kortene)|sendt?\s+(?:pm|dm|melding)|sjekk\s+(?:pm|dm|innboks(?:en)?|meldinger)|pm|dm|vipps|betal(?:ing|e)?|solgt|takk\s+for\s+(?:budet|handelen))\b/iu;
const BENIGN = /^(?:ok(?:ei|ay)?|den\s+er\s+grei|grei|greit|takk|tusen\s+takk|supert|flott|fint|nice|perfekt|[\p{Extended_Pictographic}\s!.]+)[\s!.]*$/iu;

/** Reads a seller's reply under a bid by rules: "too-late", "ok" or "unsure". `bidder`'s tag is stripped first. */
export function classifySellerReply(text: string, bidder: string | null): SellerReply["verdict"] {
  const t = stripSellerTag(text, bidder).trim();
  if (!t) return "ok"; // A photo, or only the bidder's tag.
  const late = TOO_LATE.test(t);
  const accepts = ACCEPTS.test(t);
  if (late) return accepts ? "unsure" : "too-late"; // "Avsluttet, gratulerer!" under the winner: ask.
  if (accepts || BENIGN.test(t)) return "ok";
  return "unsure";
}

/** Who a reply answers, from its aria-label: "Svar fra A på B sitt svar" / "… på B sin kommentar" / "Reply by A to B's reply". */
function replyTarget(ariaLabel: string | null): string | null {
  const m = ariaLabel?.match(/\bpå\s+(.+?)\s+(?:sitt\s+svar|sin\s+kommentar)\b/iu) ?? ariaLabel?.match(/\bto\s+(.+?)['’]s\s+(?:reply|comment)\b/iu);
  return m ? m[1] : null;
}

/**
 * The bid a seller's reply answers: by its aria-label's target (a bidder's reply or, when the post
 * is the lot, their comment), else a reply to the lot that starts with a bidder's name (a tag).
 * Then that bidder's latest bid placed before the seller's reply. Null when it answers no bid.
 */
function bidAnswered(r: CapturedReply, seller: string | null, bids: Bid[]): Bid | null {
  let target = replyTarget(r.ariaLabel);
  if (!target || (seller && normalizeName(target) === normalizeName(seller))) {
    const text = normalizeName(r.text);
    const bidders = [...new Set(bids.map((b) => b.bidder).filter(Boolean))];
    target =
      bidders.find((b) => text.startsWith(normalizeName(b))) ??
      (() => {
        const byFirst = bidders.filter((b) => text.startsWith(normalizeName(b).split(" ")[0] + " "));
        return byFirst.length === 1 ? byFirst[0] : null;
      })() ??
      null;
  }
  if (!target) return null;
  const theirs = bids.filter((b) => normalizeName(b.bidder) === normalizeName(target) && (r.id === null || compareIds(b.replyId, r.id) < 0));
  return theirs.at(-1) ?? null;
}

/** Reads the seller's replies under bids, and marks the bids they reject (in place). */
function applySellerReplies(replies: CapturedReply[], seller: string | null, bids: Bid[], options: LotOptions): void {
  if (!seller) return;
  for (const r of replies) {
    if (normalizeName(r.author) !== normalizeName(seller)) continue;
    const bid = bidAnswered(r, seller, bids);
    if (!bid) continue;
    let verdict = classifySellerReply(r.text, bid.bidder);
    let viaClaude = false;
    if (verdict === "unsure" && bid.isMe) {
      const answer = options.sellerReplyAnswer?.(r.text);
      if (answer === true || answer === false) {
        verdict = answer ? "too-late" : "ok";
        viaClaude = true;
      }
    }
    // A later reply under the same bid wins only if it says more (a rejection beats "ok").
    if (bid.sellerReply?.verdict === "too-late") continue;
    bid.sellerReply = { text: r.text, verdict, viaClaude };
    if (verdict === "too-late") {
      bid.late = "seller";
      bid.note = `seller: too late ("${r.text.trim()}")${viaClaude ? " (read by Claude)" : ""}`;
    }
  }
}

/**
 * Seller replies under your bids that the rules can't classify, for Claude (raw text kept). Each
 * is asked once (`sellerReplyAnswerKey`), whatever lot it's on.
 */
export function unsureSellerReplies(capture: PostCapture, myName: string, claims = false): string[] {
  if (claims || !myName.trim()) return [];
  const lots = interpretLots(capture, { myName, listingIncrement: null, listingMinPrice: null });
  return [
    ...new Set(
      lots.flatMap((l) => l.bids.filter((b) => b.isMe && b.sellerReply?.verdict === "unsure" && !b.sellerReply.viaClaude).map((b) => b.sellerReply!.text)),
    ),
  ];
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
