import { claimLotsToRead, interpretLots, normalizeName, untitledLotPhotos, unsureReplies, type ClaimPhotoInput } from "../domain/bids";
import { answerLookups } from "../llm/answers";
import { interpretListing } from "../domain/listing";
import { claudeEndsAt } from "../domain/endTime";
import {
  bidAnswerKey,
  claimMatchAnswerKey,
  claimPhotoAnswerKey,
  endTimeAnswerKey,
  lotNameAnswerKey,
  type BidItem,
  type ClaimMatchItem,
  type ClaudeRequest,
  type EndTimeItem,
  type LotNameItem,
} from "../llm/prompts";
import type { Store } from "../store";

// What to send to Claude, and what to leave alone: pure decisions, no chrome.* and no bridge,
// so they can be tested against an in-memory Store (tests/claude-queue.test.ts). claude.ts runs
// the requests. Review M2 (2026-10-03): one broken claim lot (its photo URL expired on the CDN)
// used to stop every run, and ended sales were still sent to Claude.

const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

/** Batched (Haiku) questions per run. */
export const MAX_END_TIMES = 20;
export const MAX_BIDS = 40;
/** Claim lots whose claims the rules couldn't match (Haiku, text only), batched. */
export const MAX_CLAIM_MATCHES = 20;
/** Photo questions are one call each (Sonnet), so fewer per run. */
export const MAX_CLAIM_LOTS = 5;
/** Lot photos to name (Sonnet): several per call, at most this many per run. */
export const LOT_NAMES_PER_CALL = 12;
export const MAX_LOT_NAMES = 24;

/**
 * Sonnet photos allowed per rolling hour, across runs and service-worker restarts: one per claim
 * lot call, and each photo of a lot-name batch. A claim sale with 60 lots would otherwise be read
 * in one go on the user's own Claude plan; the rest simply waits for later runs.
 */
export const PHOTO_CALLS_PER_HOUR = 20;

/**
 * How long after its end a sale's items are still worth asking about: bids and claims placed in
 * the last minutes are read after the end (the reader reads your auctions every 15 min, and Won/
 * Lost needs a complete read after the end), so allow a few hours, then the result is settled.
 */
export const ENDED_GRACE_MS = 6 * HOUR;
/**
 * Sales with no known end (fixed price, or an end time nobody could read): skip them once the
 * post hasn't been seen in the feed or read for this long. The first feed scan stops at 3-day-old
 * posts too (docs/spec.md "Feed scan"), so older ones are off the user's radar anyway.
 */
export const STALE_AFTER_MS = 3 * DAY;

/** Per-item failures: tries before an item is skipped, and the wait after each failed try. */
export const MAX_ATTEMPTS = 3;
const RETRY_AFTER_MS = [15 * MINUTE, 1 * HOUR];
/** Failure records are forgotten after this; by then the sale has ended and won't be asked again. */
const FAILURE_TTL_MS = 7 * DAY;

export type ClaudeTask = ClaudeRequest["task"];

/** One item (end time, bid, or claim lot) Claude couldn't answer: kept so it can't block the others. */
export type ItemFailure = { task: ClaudeTask; attempts: number; lastError: string; lastAt: string };
/** Failures by answer key (the same key the answer would be cached under). */
export type Failures = Record<string, ItemFailure>;

/**
 * Errors that hit every request, not one item: the bridge isn't installed or crashed, Claude Code
 * is missing, not logged in, or out of quota. Those pause all of Claude (a cooldown) instead of
 * counting against an item. Everything else (a photo the CDN won't serve any more, a timeout, an
 * answer that isn't JSON) is the item's own failure. Bridge errors (sendNativeMessage throwing)
 * are always global and flagged by claude.ts directly.
 */
export function isGlobalError(error: string): boolean {
  return /claude\) not found|not logged in|\/login|log in|authenticat|api key|usage limit|rate limit|limit reached|overloaded|credit balance/i.test(error);
}

/** A photo the bridge couldn't download (signed CDN URLs expire: HTTP 403/404), or refused. */
export function isPhotoError(error: string): boolean {
  return /HTTP Error \d{3}|URLError|not a Facebook image URL|image too large|redirect to .* refused/i.test(error);
}

/** "ok": ask; "waiting": failed recently, retry after a pause; "skipped": failed too often, leave it. */
export function failureStatus(f: ItemFailure | undefined, now: Date): "ok" | "waiting" | "skipped" {
  if (!f) return "ok";
  if (f.attempts >= MAX_ATTEMPTS) return "skipped";
  const wait = RETRY_AFTER_MS[Math.min(f.attempts, RETRY_AFTER_MS.length) - 1] ?? 0;
  return now.getTime() - Date.parse(f.lastAt) < wait ? "waiting" : "ok";
}

/** One more failed try for an item. Returns a new record set. */
export function recordFailure(failures: Failures, key: string, task: ClaudeTask, error: string, now: Date): Failures {
  const attempts = (failures[key]?.attempts ?? 0) + 1;
  return { ...failures, [key]: { task, attempts, lastError: error.slice(0, 300), lastAt: now.toISOString() } };
}

/** Drops old records, so the set stays small (an answered item's record is removed when it's answered). */
export function pruneFailures(failures: Failures, now: Date): Failures {
  const out: Failures = {};
  for (const [key, f] of Object.entries(failures)) {
    if (now.getTime() - Date.parse(f.lastAt) < FAILURE_TTL_MS) out[key] = f;
  }
  return out;
}

const FAILURES_META_KEY = "claudeFailures";

export async function loadFailures(store: Store): Promise<Failures> {
  try {
    return JSON.parse((await store.getMeta(FAILURES_META_KEY)) ?? "{}") as Failures;
  } catch {
    return {};
  }
}

export const saveFailures = (store: Store, failures: Failures) => store.setMeta(FAILURES_META_KEY, JSON.stringify(failures));

/** Photo calls made in the last hour (ISO times), oldest first. */
export const recentPhotoCalls = (calls: string[], now: Date) =>
  calls.filter((t) => now.getTime() - Date.parse(t) < HOUR).sort();

export const photoCallsLeft = (calls: string[], now: Date) => Math.max(0, PHOTO_CALLS_PER_HOUR - recentPhotoCalls(calls, now).length);

/** When the next photo call is allowed again (the oldest call in the window turns an hour old), or null if one is allowed now. */
export function photoLimitFreesAt(calls: string[], now: Date): string | null {
  const recent = recentPhotoCalls(calls, now);
  if (recent.length < PHOTO_CALLS_PER_HOUR) return null;
  return new Date(Date.parse(recent[recent.length - PHOTO_CALLS_PER_HOUR]) + HOUR).toISOString();
}

/**
 * Whether a sale is over for Claude's purposes: ended more than ENDED_GRACE_MS ago, or (no known
 * end) not seen or read for STALE_AFTER_MS.
 */
export function isSaleOver(endsAt: string | null, lastActivityAt: string, now: Date): boolean {
  if (endsAt) return now.getTime() > Date.parse(endsAt) + ENDED_GRACE_MS;
  return now.getTime() - Date.parse(lastActivityAt) > STALE_AFTER_MS;
}

const later = (a: string, b: string | undefined) => (b && b > a ? b : a);

export type PendingOptions = {
  myName?: string;
  now?: Date;
  failures?: Failures;
  /** Photo calls the hourly cap still allows (default: no cap). */
  photoCallsLeft?: number;
};

export type Pending = {
  endTimes: (EndTimeItem & { key: string })[];
  bids: (BidItem & { key: string })[];
  /** Claim/fixed-price lot photos to read (cards and prices), once each, yours first. */
  claimLots: (ClaimPhotoInput & { key: string })[];
  /** Read lots whose claims the rules couldn't match to the cards. */
  claimMatches: (ClaimMatchItem & { key: string })[];
  /** Lot photos whose text doesn't name the lot ("Mp 20kr"), yours first: Claude names them. */
  lotNames: (LotNameItem & { key: string; mine: boolean })[];
  /** More photos (claim lots or lot names) wait than this run takes (and the photo cap allows more): run again after it. */
  more: boolean;
  /** Photos (claim lots or lot names) held back by the hourly photo cap. */
  photoLimited: boolean;
  /** Items of live sales left alone after MAX_ATTEMPTS failures. */
  skipped: { key: string; failure: ItemFailure }[];
};

/**
 * What still needs Claude: end times the rules couldn't find in complete text, unsure bids, and
 * claim/fixed-price lot photos (yours first), and lot photos to name (posts you're in first).
 * Leaves out what's answered, sales that are over,
 * items waiting after a failure, and items that failed too often.
 */
export async function pendingItems(store: Store, opts: PendingOptions = {}): Promise<Pending> {
  const { myName = "", now = new Date(), failures = {}, photoCallsLeft: photoLeft = Infinity } = opts;
  const [posts, captures, answers] = await Promise.all([store.allPosts(), store.allCaptures(), store.allAnswers()]);
  const answerMap = new Map(answers.map((a) => [a.key, a.value]));
  const lookups = answerLookups(answerMap);
  const postsById = new Map(posts.map((p) => [p.id, p]));
  const skipped: Pending["skipped"] = [];
  const seen = new Set<string>();
  /** Not answered, not already queued, and not held back by an earlier failure. */
  const wanted = (key: string) => {
    if (answerMap.has(key) || seen.has(key)) return false;
    seen.add(key);
    const status = failureStatus(failures[key], now);
    if (status === "skipped") skipped.push({ key, failure: failures[key] });
    return status === "ok";
  };

  const endTimes: Pending["endTimes"] = [];
  for (const p of posts) {
    if (!p.textComplete) continue;
    const i = interpretListing(p.text, new Date(p.firstSeenAt));
    if (i.type !== "auction" && i.type !== "claim") continue;
    // Asked when the rules couldn't read the end, or a start line they couldn't read either.
    if (i.endsAt && !(i.startsAtText && !i.startsAt)) continue;
    // No end time is known (that's the question), so only the age limit applies.
    if (isSaleOver(null, p.lastSeenAt, now)) continue;
    const key = endTimeAnswerKey(p.text);
    if (wanted(key)) endTimes.push({ id: endTimes.length, text: p.text, capturedAt: p.firstSeenAt, key });
  }

  const bids: Pending["bids"] = [];
  const claimLots: Pending["claimLots"] = [];
  const claimMatches: Pending["claimMatches"] = [];
  const lotNames: Pending["lotNames"] = [];
  const me = normalizeName(myName);
  for (const { postId, capture } of captures) {
    const post = postsById.get(postId);
    const type = interpretListing(capture.post.text, new Date(capture.capturedAt)).type;
    // The sale's end as the overview shows it (src/pages/dashboard/model.ts): the rules on the
    // feed text from when it was first seen, else on the read's text, else Claude's answer.
    const ref = new Date(post?.firstSeenAt ?? capture.capturedAt);
    const endsAt =
      (post && interpretListing(post.text, ref).endsAt) ||
      interpretListing(capture.post.text, ref).endsAt ||
      claudeEndsAt(answerMap.get(endTimeAnswerKey(post?.text ?? capture.post.text)));
    if (isSaleOver(endsAt, later(capture.capturedAt, post?.lastSeenAt), now)) continue;

    // Auction lots whose text doesn't name them: Claude names them from the text and photo. (A claim
    // lot is named from the cards its photo read finds: no second look at the same photo.)
    const mine = !!me && capture.comments.some((c) => c.replies.some((r) => normalizeName(r.author) === me));
    for (const { imageUrl, text } of type === "claim" || type === "fixed" ? [] : untitledLotPhotos(capture)) {
      const key = lotNameAnswerKey(imageUrl, text);
      if (wanted(key)) lotNames.push({ imageUrl, text, key, mine });
    }

    if (type === "auction") {
      // Only auctions have bids to read; claim and fixed-price replies are claims.
      for (const u of unsureReplies(capture)) {
        const key = bidAnswerKey(u.seller, u.text);
        if (wanted(key)) bids.push({ id: bids.length, seller: u.seller, text: u.text, key });
      }
    } else if (type === "claim" || type === "fixed") {
      // Claim and fixed-price lots (yours first): every card and its price, read once per photo.
      for (const photo of claimLotsToRead(capture, myName)) {
        const key = claimPhotoAnswerKey(photo);
        if (wanted(key)) claimLots.push({ ...photo, key });
      }
      // Read lots whose claims the rules couldn't match: Claude matches them from the text, no names.
      const lots = interpretLots(capture, { myName, claims: true, listingIncrement: null, listingMinPrice: null, ...lookups });
      for (const l of lots) {
        if (!l.claimMatchInput) continue;
        const key = claimMatchAnswerKey(l.claimMatchInput);
        if (wanted(key)) claimMatches.push({ ...l.claimMatchInput, id: claimMatches.length, key });
      }
    }
  }

  // Photos share the hourly cap: claim lots first, lot names get what's left (one per photo).
  lotNames.sort((a, b) => Number(b.mine) - Number(a.mine));
  const take = Math.min(MAX_CLAIM_LOTS, photoLeft);
  const takeNames = Math.min(MAX_LOT_NAMES, Math.max(0, photoLeft - Math.min(take, claimLots.length)));
  const lotsHeld = claimLots.length > take;
  const namesHeld = lotNames.length > takeNames;
  return {
    endTimes: endTimes.slice(0, MAX_END_TIMES),
    bids: bids.slice(0, MAX_BIDS),
    claimLots: claimLots.slice(0, take),
    claimMatches: claimMatches.slice(0, MAX_CLAIM_MATCHES),
    lotNames: lotNames.slice(0, takeNames),
    more: (lotsHeld && take === MAX_CLAIM_LOTS) || (namesHeld && takeNames === MAX_LOT_NAMES),
    photoLimited: (lotsHeld && take < MAX_CLAIM_LOTS) || (namesHeld && takeNames < MAX_LOT_NAMES),
    skipped,
  };
}

/**
 * Lot photos to name, split into calls: up to LOT_NAMES_PER_CALL per call on a first try, but a
 * photo that already failed goes on its own, so one broken photo (an expired CDN URL fails the
 * whole call) can't keep failing the others with it.
 */
export function lotNameBatches<T extends { key: string }>(items: T[], failures: Failures): T[][] {
  const fresh = items.filter((i) => !failures[i.key]);
  const retried = items.filter((i) => failures[i.key]);
  const batches: T[][] = [];
  for (let i = 0; i < fresh.length; i += LOT_NAMES_PER_CALL) batches.push(fresh.slice(i, i + LOT_NAMES_PER_CALL));
  for (const item of retried) batches.push([item]);
  return batches;
}

const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? "" : "s"}`;

/** One run's outcome for the overview's Claude status line. */
export type RunSummary = {
  read: { endTimes: number; bids: number; claimLots: number; claimMatches: number; lotNames: number };
  /** Items that failed this run and will be tried again later. */
  failed: number;
  /** Items of live sales skipped after MAX_ATTEMPTS failures (including any that just reached it). */
  skipped: ItemFailure[];
  photoLimited: boolean;
};

/** "Read 5 claim lot photos · 2 skipped (photo unavailable) · photo limit reached, rest later". */
export function describeRun(s: RunSummary): string {
  const read = [
    s.read.endTimes && plural(s.read.endTimes, "end time"),
    s.read.bids && plural(s.read.bids, "bid"),
    s.read.claimLots && plural(s.read.claimLots, "claim lot photo"),
    s.read.claimMatches && `claims on ${plural(s.read.claimMatches, "lot")}`,
    s.read.lotNames && `${plural(s.read.lotNames, "lot name")} from photos`,
  ].filter(Boolean);
  const parts = [read.length ? `Read ${read.join(" and ")}` : "Nothing read"];
  if (s.failed) parts.push(`${s.failed} failed, will retry`);
  const photo = s.skipped.filter((f) => isPhotoError(f.lastError)).length;
  if (photo) parts.push(`${photo} skipped (photo unavailable)`);
  if (s.skipped.length - photo) parts.push(`${s.skipped.length - photo} skipped (failed ${MAX_ATTEMPTS} times)`);
  if (s.photoLimited) parts.push("photo limit reached, rest later");
  return parts.join(" · ");
}
