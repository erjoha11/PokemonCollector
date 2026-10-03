import type { CapturedComment, CapturedReply, PostCapture } from "../shared/capture";
import { parseAmount } from "./amount";

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

export type MyStatus = "none" | "lead" | "outbid";

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

/** Your claim on a lot: first on what you named, someone was earlier, or no claim. */
export type MyClaim = "none" | "claimed" | "check";

export type Lot = {
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

/** Reply IDs are large numbers that increase with time; compare without losing precision. */
function compareIds(a: string | null, b: string | null): number {
  if (a === null || b === null) return 0;
  if (a.length !== b.length) return a.length - b.length;
  return a < b ? -1 : a > b ? 1 : 0;
}

/** "MP: 1400", "Mp 10kr", "Holo, mp 30kr", "Minstepris 500", or a bare "700kr" on its own line. */
function lotStartBid(text: string): number | null {
  const m = text.match(/(?<![\p{L}\d])(?:mp|minstepris|startbud)\s*:?\s*(\d[^\n]*)/iu);
  if (m) return parseAmount(m[1]);
  const bare = text.match(/(?:^|\n)\s*(\d[\d .]*)\s*(?:kr|,-)\s*(?:\n|$)/i);
  return bare ? parseAmount(bare[1]) : null;
}

/** The lot's first line, unless it's only a price ("Mp 15kr"); then "Lot N". */
function lotTitle(text: string, position: number): string {
  const first = text.split("\n")[0]?.trim() ?? "";
  const onlyPrice = /^(?:mp|mb|minstepris)?\s*:?\s*\d[\d .,]*\s*(?:kr|,-|&)?\.?$/i.test(first);
  return first && !onlyPrice ? first : `Lot ${position}`;
}

/** "MB: 10" (minimum increment for this lot). */
function lotIncrement(text: string): number | null {
  const m = text.match(/(?:^|\n)\s*(?:mb|min(?:imum)?\.?\s*bud(?:økning)?)\s*:?\s*([^\n]+)/i);
  return m ? parseAmount(m[1]) : null;
}

export type LotOptions = {
  myName: string;
  /** Claim sales and fixed-price posts: replies are claims, not bids. */
  claims?: boolean;
  /** From the post: used when a lot doesn't state its own. */
  listingIncrement: number | null;
  listingMinPrice: number | null;
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

export function interpretLots(capture: PostCapture, options: LotOptions): Lot[] {
  const seller = sellerOf(capture);
  const me = normalizeName(options.myName);
  const lots: Lot[] = [];
  for (const c of capture.comments) {
    if (!isLot(c, seller)) continue;
    const startBid = lotStartBid(c.text) ?? options.listingMinPrice;
    const increment = lotIncrement(c.text) ?? options.listingIncrement;
    const replies = [...c.replies].sort((a, b) => compareIds(a.id, b.id));
    const bids: Bid[] = [];
    let unsureCount = 0;
    const claims = options.claims ? readClaims(replies, seller, me) : [];
    const mineClaims = claims.filter((x) => x.isMe);
    const myClaim: MyClaim = mineClaims.length === 0 ? "none" : mineClaims.some((x) => !x.contested) ? "claimed" : "check";
    for (const r of options.claims ? [] : replies) {
      const bid = toBid(r, seller, me, options);
      if (bid === "unsure") unsureCount++;
      else if (bid) bids.push(bid);
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
      title: lotTitle(c.text, lots.length + 1),
      rawText: c.text,
      imageUrl: c.images[0]?.src ?? null,
      startBid,
      increment,
      bids,
      highestBid: highest?.amount ?? null,
      highestBidder: highest?.bidder ?? null,
      myHighestBid,
      myStatus: mine.length === 0 ? "none" : highest?.isMe ? "lead" : "outbid",
      unsureCount,
      belowStart,
      claims,
      myClaim,
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

function toBid(r: CapturedReply, seller: string | null, me: string, options: LotOptions): Bid | "unsure" | null {
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
  const underReply = /\bsitt svar\b|'s reply\b/i.test(r.ariaLabel ?? "");
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
