import { interpretLots, summarizeLots, type Lot } from "../domain/bids";
import { interpretListing, type Interpretation, type SaleType } from "../domain/listing";
import { saleLines } from "../domain/saleLines";
import { saleClosesAt } from "../domain/bidTime";
import { bidAnswerKey, lotNameAnswerKey, sellerReplyAnswerKey } from "../llm/prompts";
import type { PostCapture } from "../shared/capture";
import type { StoredPost } from "../shared/feed";
import { canonicalPostUrl } from "../shared/urls";

// Auctions you're in, and what to do about them: which to re-read now, when the next final read
// is due (just after a sale closes, so Leading becomes Won or Lost without you opening it), and
// which end within 10 minutes (a notification). Pure; reader.ts and notify.ts act on it.

/** A running auction you're in is re-read this often (docs/spec.md "Slow pacing"). */
export const REREAD_AFTER_MS = 15 * 60_000;
/** The final read waits this long after the close (end + antisnipe), for the last replies to load. */
export const FINAL_READ_GRACE_MS = 2 * 60_000;
/** After that, a final read is still worth trying this long (when it couldn't run on time). */
export const FINAL_READ_WITHIN_MS = 2 * 60 * 60_000;
/** "Ends in 10 min" notification. */
export const ENDING_SOON_MS = 10 * 60_000;

export type MyAuction = {
  postId: string;
  url: string;
  /** The sale's name without the template's type words. */
  title: string;
  /** Always "auction" today (only auctions are watched); named in notifications. */
  type: SaleType;
  endsAt: number | null;
  /** End plus antisnipe: after this, no bid counts. */
  closesAt: number | null;
  lastRead: number;
  /** The last read that loaded every comment (0 when none did). */
  lastComplete: number;
  lots: Lot[];
};

export type AnswerMap = Map<string, unknown>;

/** A post's listing and its lots as you see them (your bids, the highest), from one read. */
export function readLots(post: Pick<StoredPost, "text" | "firstSeenAt">, capture: PostCapture, answers: AnswerMap, myName: string): { listing: Interpretation; lots: Lot[] } {
  const listing = interpretListing(post.text, new Date(post.firstSeenAt));
  const lots = interpretLots(capture, {
    myName,
    listingIncrement: listing.increment,
    listingMinPrice: listing.minPrice,
    endsAt: listing.endsAt ? Date.parse(listing.endsAt) : null,
    softCloseMinutes: listing.softCloseMinutes,
    lotName: (imageUrl) => answers.get(lotNameAnswerKey(imageUrl)) as string | null | undefined,
    answer: (seller, text) => {
      const key = bidAnswerKey(seller, text);
      return answers.has(key) ? (answers.get(key) as number | null) : undefined;
    },
    sellerReplyAnswer: (text) => answers.get(sellerReplyAnswerKey(text)) as boolean | null | undefined,
  });
  return { listing, lots };
}

/** When an auction is over: the end with chained antisnipe over its lots' bids (src/domain/bidTime.ts, #329). */
export function closesAtOf(listing: Pick<Interpretation, "endsAt" | "softCloseMinutes">, lots: Lot[]): number | null {
  return saleClosesAt(listing.endsAt ? Date.parse(listing.endsAt) : null, listing.softCloseMinutes, lots.map((l) => l.closesAt));
}

/** When a read last loaded every comment. Reads saved before merging existed count as complete. */
export const completeAt = (c: PostCapture): number =>
  c.completeAt === undefined ? Date.parse(c.capturedAt) : c.completeAt ? Date.parse(c.completeAt) : 0;

/** Was this read in full after the sale closed? Then its result is final. */
export const isFinal = (c: PostCapture, closesAt: number | null): boolean => closesAt !== null && completeAt(c) >= closesAt;

/** Auctions you've bid in (leading, outbid, or unclear at their latest read). */
export function myAuctions(posts: StoredPost[], captures: { postId: string; capture: PostCapture }[], answers: AnswerMap, myName: string): MyAuction[] {
  const byId = new Map(posts.map((p) => [p.id, p]));
  const out: MyAuction[] = [];
  for (const { postId, capture } of captures) {
    const post = byId.get(postId);
    if (!post) continue;
    const { listing, lots } = readLots(post, capture, answers, myName);
    if (listing.type !== "auction") continue;
    const s = summarizeLots(lots);
    if (s.lead + s.outbid + s.unclear === 0) continue;
    const endsAt = listing.endsAt ? Date.parse(listing.endsAt) : null;
    out.push({
      postId,
      url: canonicalPostUrl(post.url, postId, post.groupSlug),
      title: saleLines(listing.title, listing.description).title,
      type: listing.type,
      endsAt,
      closesAt: closesAtOf(listing, lots),
      lastRead: Date.parse(capture.capturedAt),
      lastComplete: completeAt(capture),
      lots,
    });
  }
  return out;
}

export type WatchPlan = {
  /** Re-read now: running ones not read for 15 min, and closed ones without a final read yet. */
  due: MyAuction[];
  /** When the next final read falls due (a sale closes + grace), or null. */
  nextFinalAt: number | null;
  /** Ending within 10 minutes (and not yet ended): notify. */
  endingSoon: MyAuction[];
  /** When the next one enters its last 10 minutes, or null. */
  nextEndingSoonAt: number | null;
};

export function watchPlan(auctions: MyAuction[], now: number): WatchPlan {
  const due: MyAuction[] = [];
  const endingSoon: MyAuction[] = [];
  let nextFinalAt: number | null = null;
  let nextEndingSoonAt: number | null = null;
  const earliest = (a: number | null, b: number) => (a === null || b < a ? b : a);
  for (const a of auctions) {
    const closed = a.closesAt !== null && now >= a.closesAt;
    if (closed) {
      // A complete read after the close settles it (a partial one is tried again, review H2).
      const settled = a.lastComplete >= a.closesAt!;
      const finalAt = a.closesAt! + FINAL_READ_GRACE_MS;
      if (settled || now - a.closesAt! > FINAL_READ_WITHIN_MS) continue;
      if (now >= finalAt) due.push(a);
      else nextFinalAt = earliest(nextFinalAt, finalAt);
      continue;
    }
    if (now - a.lastRead >= REREAD_AFTER_MS) due.push(a);
    if (a.closesAt !== null) nextFinalAt = earliest(nextFinalAt, a.closesAt + FINAL_READ_GRACE_MS);
    if (a.endsAt !== null) {
      const soonAt = a.endsAt - ENDING_SOON_MS;
      if (now >= soonAt && now < a.endsAt) endingSoon.push(a);
      else if (now < soonAt) nextEndingSoonAt = earliest(nextEndingSoonAt, soonAt);
    }
  }
  return { due, nextFinalAt, endingSoon, nextEndingSoonAt };
}
