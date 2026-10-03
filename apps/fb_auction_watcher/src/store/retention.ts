import { claimLotAnswerKey, bidAnswerKey, endTimeAnswerKey, type ClaimLotAnswer } from "../llm/prompts";
import { claimLotsToRead, interpretLots, summarizeLots, unsureReplies } from "../domain/bids";
import { claudeEndsAt } from "../domain/endTime";
import { interpretListing } from "../domain/listing";
import type { PostCapture } from "../shared/capture";
import type { StoredPost } from "../shared/feed";
import type { Store, StoredAnswer, StoredCapture } from "./index";

// Retention (review M6): what's stored is mostly other people's data (sellers' posts, bidders'
// names and replies, Claude's readings of them), so it's kept only as long as it's useful:
//
// - A post read (comments, replies, names) is deleted 7 days after its sale ended, or 30 days
//   if you bid or claimed in it (My Auctions' "Ended" list is what you pay and follow up from).
//   A sale with no end time (fixed price, or an end the rules couldn't read) counts from when it
//   was last seen in the feed or read, with the 14-day window (30 if yours).
// - A post (the slim row: its text, seller, link) is deleted once its read is gone (or it never
//   had one), it hasn't been seen in the feed for 14 days, and its sale ended over 7 days ago or
//   has no end time.
// - A Claude answer is deleted once nothing kept refers to it any more. Answers still in use stay
//   whatever their age: deleting one would only send the same question to Claude again.
// - A sale that hasn't ended is never touched.
//
// Pure: everything comes in as arguments (including `now`), so it's tested with invented data.

const DAY = 86_400_000;

export const RETENTION = {
  /** Days after a sale ended that its post read is kept. */
  readDaysAfterEnd: 7,
  /** The same for sales in your My Auctions (bid or claimed). */
  myReadDays: 30,
  /** Days a post with nothing else keeping it stays after it was last seen in the feed. */
  unseenDays: 14,
} as const;

export type RetentionData = { posts: StoredPost[]; captures: StoredCapture[]; answers: StoredAnswer[] };
/** What to delete: post IDs, post-read IDs (= post IDs), answer keys. */
export type RetentionPlan = { posts: string[]; captures: string[]; answers: string[] };

/** Is this sale in your My Auctions? Same rules as the overview: any lot you bid on or claimed. */
function isMine(capture: PostCapture, text: string, firstSeenAt: string, myName: string, answers: Map<string, unknown>): boolean {
  if (!myName.trim()) return false;
  const listing = interpretListing(text, new Date(firstSeenAt));
  const lots = interpretLots(capture, {
    myName,
    claims: listing.type === "claim" || listing.type === "fixed",
    listingIncrement: listing.increment,
    listingMinPrice: listing.minPrice,
    claimAnswer: (input) => answers.get(claimLotAnswerKey(input)) as ClaimLotAnswer | undefined,
    answer: (seller, t) => {
      const key = bidAnswerKey(seller, t);
      return answers.has(key) ? (answers.get(key) as number | null) : undefined;
    },
  });
  const s = summarizeLots(lots);
  return s.lead + s.outbid + s.unclear + s.claimed + s.check > 0;
}

/** Every Claude answer key a post or its read can look up (the same helpers that ask Claude). */
function answerKeysFor(post: StoredPost | null, capture: PostCapture | null, myName: string): string[] {
  const keys: string[] = [];
  if (post) keys.push(endTimeAnswerKey(post.text));
  if (capture) {
    keys.push(endTimeAnswerKey(capture.post.text));
    for (const u of unsureReplies(capture)) keys.push(bidAnswerKey(u.seller, u.text));
    for (const lot of claimLotsToRead(capture, myName)) keys.push(claimLotAnswerKey(lot));
  }
  return keys;
}

export function planRetention(data: RetentionData, myName: string, now: Date): RetentionPlan {
  const t = now.getTime();
  const answers = new Map(data.answers.map((a) => [a.key, a.value]));
  const posts = new Map(data.posts.map((p) => [p.id, p]));
  const captures = new Map(data.captures.map((c) => [c.postId, c.capture]));
  const plan: RetentionPlan = { posts: [], captures: [], answers: [] };
  const usedKeys = new Set<string>();

  for (const id of new Set([...posts.keys(), ...captures.keys()])) {
    const post = posts.get(id) ?? null;
    const capture = captures.get(id) ?? null;
    // A read without a post row (shouldn't happen, but don't trip over it): judge it by its own text.
    const text = post?.text ?? capture!.post.text;
    const firstSeenAt = post?.firstSeenAt ?? capture!.capturedAt;

    // When the sale closes: its end time (the rules', else Claude's) plus any antisnipe window.
    const listing = interpretListing(text, new Date(firstSeenAt));
    const endsAt = listing.endsAt ?? claudeEndsAt(answers.get(endTimeAnswerKey(text)));
    const closesAt = endsAt ? Date.parse(endsAt) + (listing.softCloseMinutes ?? 0) * 60_000 : null;

    let keepPost = true;
    let keepCapture = true;
    if (closesAt === null || t >= closesAt) {
      // Ended, or no end time: count from the end, else from when it was last seen or read.
      const lastActivity = Math.max(post ? Date.parse(post.lastSeenAt) : 0, capture ? Date.parse(capture.capturedAt) : 0);
      const mine = capture !== null && isMine(capture, text, firstSeenAt, myName, answers);
      if (capture) {
        const age = closesAt !== null ? t - closesAt : t - lastActivity;
        const keepDays = mine ? RETENTION.myReadDays : closesAt !== null ? RETENTION.readDaysAfterEnd : RETENTION.unseenDays;
        keepCapture = age <= keepDays * DAY;
      }
      if (post) {
        const unseenLong = t - Date.parse(post.lastSeenAt) > RETENTION.unseenDays * DAY;
        const endedLong = closesAt === null || t - closesAt > RETENTION.readDaysAfterEnd * DAY;
        keepPost = (capture !== null && keepCapture) || !unseenLong || !endedLong;
      }
    }

    if (post && !keepPost) plan.posts.push(id);
    if (capture && !keepCapture) plan.captures.push(id);
    for (const key of answerKeysFor(keepPost ? post : null, keepCapture ? capture : null, myName)) usedKeys.add(key);
  }

  plan.answers = data.answers.filter((a) => !usedKeys.has(a.key)).map((a) => a.key);
  return plan;
}

export type RetentionResult = { captures: number; posts: number; answers: number };

/** Runs the rule against a store and deletes what it says. Returns how much went. */
export async function applyRetention(store: Store, myName: string, now: Date): Promise<RetentionResult> {
  const [posts, captures, answers] = await Promise.all([store.allPosts(), store.allCaptures(), store.allAnswers()]);
  const plan = planRetention({ posts, captures, answers }, myName, now);
  await store.deleteCaptures(plan.captures);
  await store.deletePosts(plan.posts);
  await store.deleteAnswers(plan.answers);
  return { captures: plan.captures.length, posts: plan.posts.length, answers: plan.answers.length };
}

/** "Removed 12 old post reads, 30 posts and 4 Claude answers", or "Nothing old to remove". */
export function describeRetention(r: RetentionResult): string {
  const n = (count: number, one: string, many: string) => `${count} ${count === 1 ? one : many}`;
  const parts = [
    r.captures ? n(r.captures, "old post read", "old post reads") : null,
    r.posts ? n(r.posts, "post", "posts") : null,
    r.answers ? n(r.answers, "Claude answer", "Claude answers") : null,
  ].filter((p): p is string => p !== null);
  if (parts.length === 0) return "Nothing old to remove";
  return `Removed ${parts.length > 1 ? `${parts.slice(0, -1).join(", ")} and ${parts.at(-1)}` : parts[0]}`;
}
